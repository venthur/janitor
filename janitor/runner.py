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

import aiozipkin
import asyncio
import asyncpg
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from email.utils import parseaddr
import functools
import json
from io import BytesIO
import logging
import os
import shlex
import ssl
import sys
import tempfile
from typing import List, Any, Optional, BinaryIO, Dict, Tuple, Type
import uuid

from aiohttp import web

from yarl import URL

from breezy import debug, urlutils
from breezy.branch import Branch
from breezy.errors import PermissionDenied, ConnectionError
from breezy.transport import UnusableRedirect

from prometheus_client import Counter, Gauge, Histogram

from silver_platter.debian import (
    select_preferred_probers,
)
from silver_platter.proposal import (
    find_existing_proposed,
    UnsupportedHoster,
    HosterLoginRequired,
    NoSuchProject,
    get_hoster,
)
from silver_platter.utils import (
    BranchRateLimited,
    full_branch_url,
)

from . import (
    state,
)
from .compat import shlex_join
from .artifacts import (
    get_artifact_manager,
    LocalArtifactManager,
    store_artifacts_with_backup,
    upload_backup_artifacts,
)
from .config import read_config, get_suite_config
from .debian import (
    changes_filenames,
    open_guessed_salsa_branch,
    find_changes,
    NoChangesFile,
    dpkg_vendor,
)
from .logs import (
    get_log_manager,
    ServiceUnavailable,
    LogFileManager,
    FileSystemLogFileManager,
)
from .policy import read_policy
from .prometheus import setup_metrics
from .pubsub import Topic, pubsub_handler
from .schedule import do_schedule_control, do_schedule
from .vcs import (
    get_vcs_abbreviation,
    is_authenticated_url,
    open_branch_ext,
    BranchOpenFailure,
    get_vcs_manager,
    UnsupportedVcs,
)

DEFAULT_RETRY_AFTER = 120


try:
    from asyncio import to_thread  # type: ignore
except ImportError:  # python < 3.8
    from asyncio import events
    import contextvars

    async def to_thread(func, *args, **kwargs):  # type: ignore
        loop = events.get_running_loop()
        ctx = contextvars.copy_context()
        func_call = functools.partial(ctx.run, func, *args, **kwargs)
        return await loop.run_in_executor(None, func_call)


routes = web.RouteTableDef()
packages_processed_count = Counter("package_count", "Number of packages processed.")
last_success_gauge = Gauge(
    "job_last_success_unixtime", "Last time a batch job successfully finished"
)
build_duration = Histogram("build_duration", "Build duration", ["package", "suite"])
run_result_count = Counter("result", "Result counts", ["package", "suite", "result_code"])
active_run_count = Gauge("active_runs", "Number of active runs")
rate_limited_count = Counter("rate_limited_host", "Rate limiting per host", ["host"])


class BuilderResult(object):

    kind: str

    def from_directory(self, path):
        raise NotImplementedError(self.from_directory)

    async def store(self, conn, run_id):
        raise NotImplementedError(self.store)

    def json(self):
        raise NotImplementedError(self.json)

    def artifact_filenames(self):
        raise NotImplementedError(self.artifact_filenames)


class Builder(object):
    """Abstract builder class."""

    kind: str

    result_cls: Type[BuilderResult] = BuilderResult

    async def build_env(self, conn, suite_config, queue_item):
        raise NotImplementedError(self.build_env)


class GenericResult(BuilderResult):
    """Generic build result."""

    kind = "generic"

    @classmethod
    def from_json(cls, target_details):
        return cls()

    def from_directory(self, path):
        pass

    def json(self):
        return {}

    def artifact_filenames(self):
        return []

    async def store(self, conn, run_id):
        pass


class GenericBuilder(Builder):
    """Generic builder."""

    kind = "generic"

    result_cls = GenericResult

    def __init__(self, distro_config):
        self.distro_config = distro_config

    async def build_env(self, conn, suite_config, queue_item):
        env = {}
        if suite_config.generic_build.chroot:
            env["CHROOT"] = suite_config.generic_build.chroot
        elif self.distro_config.chroot:
            env["CHROOT"] = self.distro_config.chroot

        env["REPOSITORIES"] = "%s %s/ %s" % (
            self.distro_config.archive_mirror_uri,
            self.distro_config.name,
            " ".join(self.distro_config.component),
        )
        return env


class DebianResult(BuilderResult):

    kind = "debian"

    def __init__(
        self, source=None, build_version=None, build_distribution=None,
        changes_filenames=None, lintian_result=None, binary_packages=None
    ):
        self.source = source
        self.build_version = build_version
        self.build_distribution = build_distribution
        self.binary_packages = binary_packages
        self.changes_filenames = changes_filenames
        self.lintian_result = lintian_result

    def from_directory(self, path):
        try:
            self.output_directory = path
            (
                self.changes_filenames,
                self.source,
                self.build_version,
                self.build_distribution,
                self.binary_packages
            ) = find_changes(path)
        except NoChangesFile as e:
            # Oh, well.
            logging.info("No changes file found: %s", e)
        else:
            logging.info(
                "Found changes files %r, source %s, build version %s, "
                "distribution: %s, binary packages: %r",
                self.source, self.changes_filenames, self.build_version,
                self.build_distribution, self.binary_packages)

    def artifact_filenames(self):
        if not self.changes_filenames:
            return []
        ret = []
        for changes_filename in self.changes_filenames:
            changes_path = os.path.join(self.output_directory, changes_filename)
            ret.extend(changes_filenames(changes_path))
            ret.append(changes_filename)
        return ret

    @classmethod
    def from_json(cls, target_details):
        return cls(lintian_result=target_details.get('lintian'))

    async def store(self, conn, run_id):
        if self.build_version:
            await conn.execute(
                "INSERT INTO debian_build (run_id, source, version, distribution, lintian_result, binary_packages) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                run_id,
                self.source,
                self.build_version,
                self.build_distribution,
                self.lintian_result,
                self.binary_packages
            )

    def json(self):
        return {
            "build_distribution": self.build_distribution,
            "build_version": self.build_version,
            "changes_filenames": self.changes_filenames,
            "lintian": self.lintian_result,
            "binary_packages": self.binary_packages,
        }

    def __bool__(self):
        return self.changes_filenames is not None


class DebianBuilder(Builder):

    kind = "debian"

    result_cls = DebianResult

    def __init__(self, distro_config, apt_location):
        self.distro_config = distro_config
        self.apt_location = apt_location

    async def build_env(self, conn, suite_config, queue_item):
        if self.apt_location.startswith("gs://"):
            bucket_name = URL(self.apt_location).host
            apt_location = "https://storage.googleapis.com/%s/" % bucket_name
        else:
            apt_location = self.apt_location
        env = {
            "EXTRA_REPOSITORIES": ":".join(
                [
                    "deb %s %s/ main" % (apt_location, suite)
                    for suite in suite_config.debian_build.extra_build_distribution
                ]
            )
        }

        if suite_config.debian_build.chroot:
            env["CHROOT"] = suite_config.debian_build.chroot
        elif self.distro_config.chroot:
            env["CHROOT"] = self.distro_config.chroot

        if self.distro_config.name:
            env["DISTRIBUTION"] = self.distro_config.name

        env["REPOSITORIES"] = "%s %s/ %s" % (
            self.distro_config.archive_mirror_uri,
            self.distro_config.name,
            " ".join(self.distro_config.component),
        )

        env["BUILD_DISTRIBUTION"] = suite_config.debian_build.build_distribution or suite_config.name
        env["BUILD_SUFFIX"] = suite_config.debian_build.build_suffix or ""

        if suite_config.debian_build.build_command:
            env["BUILD_COMMAND"] = suite_config.debian_build.build_command
        elif self.distro_config.build_command:
            env["BUILD_COMMAND"] = self.distro_config.build_command

        last_build_version = await conn.fetchval(
            "SELECT version FROM debian_build WHERE "
            "version IS NOT NULL AND source = $1 AND "
            "distribution = $2 ORDER BY version DESC LIMIT 1",
            queue_item.package, env['BUILD_DISTRIBUTION']
        )

        if last_build_version:
            env["LAST_BUILD_VERSION"] = str(last_build_version)

        env['LINTIAN_PROFILE'] = self.distro_config.lintian_profile
        if self.distro_config.lintian_suppress_tag:
            env['LINTIAN_SUPPRESS_TAGS'] = ','.join(self.distro_config.lintian_suppress_tag)

        env.update([(env.key, env.value) for env in suite_config.debian_build.sbuild_env])

        env['DEB_VENDOR'] = self.distro_config.vendor or dpkg_vendor()

        return env


BUILDER_CLASSES: List[Type[Builder]] = [DebianBuilder, GenericBuilder]
RESULT_CLASSES = [builder_cls.result_cls for builder_cls in BUILDER_CLASSES]


def get_builder(config, suite_config):
    if suite_config.HasField('debian_build'):
        return DebianBuilder(
            config.distribution,
            config.apt_location
            )
    elif suite_config.HasField('generic_build'):
        return GenericBuilder(config.distribution)
    else:
        raise NotImplementedError('no supported build type')


class JanitorResult(object):
    def __init__(
        self,
        pkg,
        log_id,
        branch_url,
        description=None,
        code=None,
        worker_result=None,
        worker_cls=None,
        logfilenames=None,
        suite=None,
        start_time=None,
        finish_time=None,
        worker_name=None,
        worker_link=None,
        vcs_type=None,
    ):
        self.package = pkg
        self.suite = suite
        self.log_id = log_id
        self.description = description
        self.branch_url = branch_url
        self.code = code
        self.logfilenames = logfilenames or []
        self.worker_name = worker_name
        self.worker_link = worker_link
        self.vcs_type = vcs_type
        if worker_result:
            self.context = worker_result.context
            if self.code is None:
                self.code = worker_result.code or 'success'
            if self.description is None:
                self.description = worker_result.description
            self.main_branch_revision = worker_result.main_branch_revision
            self.subworker_result = worker_result.subworker
            self.revision = worker_result.revision
            self.value = worker_result.value
            self.builder_result = worker_result.builder_result
            self.branches = worker_result.branches
            self.tags = worker_result.tags
            self.remotes = worker_result.remotes
            self.failure_details = worker_result.details
            self.start_time = worker_result.start_time
            self.finish_time = worker_result.finish_time
            self.followup_actions = worker_result.followup_actions
        else:
            self.start_time = start_time
            self.finish_time = finish_time
            self.context = None
            self.main_branch_revision = None
            self.revision = None
            self.subworker_result = None
            self.value = None
            self.builder_result = None
            self.branches = None
            self.tags = None
            self.failure_details = None
            self.remotes = {}
            self.followup_actions = []

    @property
    def duration(self):
        return self.finish_time - self.start_time

    def json(self):
        return {
            "package": self.package,
            "suite": self.suite,
            "log_id": self.log_id,
            "description": self.description,
            "code": self.code,
            "failure_details": self.failure_details,
            "target": ({
                "name": self.builder_result.kind,
                "details": self.builder_result.json(),
            } if self.builder_result else {}),
            "logfilenames": self.logfilenames,
            "subworker": self.subworker_result,
            "value": self.value,
            "remotes": self.remotes,
            "branches": (
                [
                    (fn, n, br.decode("utf-8"), r.decode("utf-8"))
                    for (fn, n, br, r) in self.branches
                ]
                if self.branches is not None
                else None
            ),
            "tags": (
                [(n, r.decode("utf-8")) for (n, r) in self.tags]
                if self.tags is not None
                else None
            ),
            "revision": self.revision.decode("utf-8") if self.revision else None,
            "main_branch_revision": self.main_branch_revision.decode("utf-8")
            if self.main_branch_revision
            else None,
        }


def committer_env(committer):
    env = {}
    if not committer:
        return env
    (user, email) = parseaddr(committer)
    if user:
        env["DEBFULLNAME"] = user
    if email:
        env["DEBEMAIL"] = email
    env["COMMITTER"] = committer
    return env


class WorkerResult(object):
    """The result from a worker."""

    def __init__(
        self,
        code,
        description,
        context=None,
        subworker=None,
        main_branch_revision=None,
        revision=None,
        value=None,
        branches=None,
        tags=None,
        remotes=None,
        details=None,
        builder_result=None,
        start_time=None,
        finish_time=None,
        queue_id=None,
        worker_name=None,
        followup_actions=None,
    ):
        self.code = code
        self.description = description
        self.context = context
        self.subworker = subworker
        self.main_branch_revision = main_branch_revision
        self.revision = revision
        self.value = value
        self.branches = branches
        self.tags = tags
        self.remotes = remotes
        self.details = details
        self.builder_result = builder_result
        self.start_time = start_time
        self.finish_time = finish_time
        self.queue_id = queue_id
        self.worker_name = worker_name
        self.followup_actions = followup_actions

    @classmethod
    def from_file(cls, path):
        """create a WorkerResult object from a JSON file."""
        with open(path, "r") as f:
            worker_result = json.load(f)
        return cls.from_json(worker_result)

    @classmethod
    def from_json(cls, worker_result):
        main_branch_revision = worker_result.get("main_branch_revision")
        if main_branch_revision is not None:
            main_branch_revision = main_branch_revision.encode("utf-8")
        revision = worker_result.get("revision")
        if revision is not None:
            revision = revision.encode("utf-8")
        branches = worker_result.get("branches")
        tags = worker_result.get("tags")
        if branches:
            branches = [
                (fn, n, br.encode("utf-8") if br else None, r.encode("utf-8"))
                for (fn, n, br, r) in branches
            ]
        if tags:
            tags = [(n, r.encode("utf-8")) for (fn, n, r) in tags]
        target_kind = worker_result.get("target", {}).get("name")
        for result_cls in RESULT_CLASSES:
            if target_kind == result_cls.kind:
                target_details = worker_result["target"]["details"]
                if target_details is not None:
                    builder_result = result_cls.from_json(target_details)
                else:
                    builder_result = None
                break
        else:
            if target_kind is None:
                builder_result = None
            else:
                raise NotImplementedError('unsupported build target %r' % target_kind)
        return cls(
            worker_result.get("code"),
            worker_result.get("description"),
            worker_result.get("context"),
            worker_result.get("subworker"),
            main_branch_revision,
            revision,
            worker_result.get("value"),
            branches,
            tags,
            worker_result.get("remotes"),
            worker_result.get("details"),
            builder_result,
            datetime.fromisoformat(worker_result['start_time'])
            if 'start_time' in worker_result else None,
            datetime.fromisoformat(worker_result['finish_time'])
            if 'finish_time' in worker_result else None,
            worker_result.get("queue_id"),
            worker_result.get("worker_name"),
            worker_result.get("followup_actions")
        )


async def update_branch_url(
    conn: asyncpg.Connection, package: str, vcs_type: str, vcs_url: str
) -> None:
    await conn.execute(
        "update package set vcs_type = $1, branch_url = $2 " "where name = $3",
        vcs_type.lower(),
        vcs_url,
        package,
    )


async def open_branch_with_fallback(
    conn, pkg, vcs_type, vcs_url, possible_transports=None
):
    probers = select_preferred_probers(vcs_type)
    logging.info('Opening branch %s with %r', vcs_url, probers)
    try:
        return await to_thread(
            open_branch_ext,
            vcs_url, possible_transports=possible_transports, probers=probers)
    except BranchOpenFailure as e:
        if e.code == "hosted-on-alioth":
            logging.info(
                "Branch %s is hosted on alioth. Trying some other options..", vcs_url
            )
            try:
                branch = await open_guessed_salsa_branch(
                    conn,
                    pkg,
                    vcs_type,
                    vcs_url,
                    possible_transports=possible_transports,
                )
            except BranchOpenFailure:
                raise e
            else:
                if branch:
                    await update_branch_url(
                        conn, pkg, "Git", full_branch_url(branch).rstrip("/")
                    )
                    return branch
        raise


async def import_logs(
    output_directory: str,
    logfile_manager: LogFileManager,
    backup_logfile_manager: Optional[LogFileManager],
    pkg: str,
    log_id: str,
) -> List[str]:
    logfilenames = []
    for entry in os.scandir(output_directory):
        if entry.is_dir():
            continue
        parts = entry.name.split(".")
        if parts[-1] == "log" or (
            len(parts) == 3 and parts[-2] == "log" and parts[-1].isdigit()
        ):
            try:
                await logfile_manager.import_log(pkg, log_id, entry.path)
            except ServiceUnavailable as e:
                logging.warning("Unable to upload logfile %s: %s", entry.name, e)
                if backup_logfile_manager:
                    await backup_logfile_manager.import_log(pkg, log_id, entry.path)
            except PermissionDenied as e:
                logging.warning(
                    "Permission denied error while uploading logfile %s: %s",
                    entry.name, e)
                if backup_logfile_manager:
                    await backup_logfile_manager.import_log(pkg, log_id, entry.path)
            logfilenames.append(entry.name)
    return logfilenames


class ActiveRun(object):

    KEEPALIVE_INTERVAL = 60 * 10

    log_files: Dict[str, BinaryIO]
    worker_name: str
    queue_item: state.QueueItem
    log_id: str
    start_time: datetime

    def __init__(
        self,
        queue_item: state.QueueItem,
        worker_name: str,
        jenkins_metadata: Optional[Dict[str, str]] = None,
    ):
        self.queue_item = queue_item
        self.start_time = datetime.utcnow()
        self.log_id = str(uuid.uuid4())
        self.worker_name = worker_name
        self.log_files = {}
        self.main_branch_url = self.queue_item.branch_url
        self.vcs_type = self.queue_item.vcs_type
        self.resume_branch_name = None
        self.reset_keepalive()
        self._watch_dog = None
        self._jenkins_metadata = jenkins_metadata

    @property
    def worker_link(self):
        if self._jenkins_metadata is not None:
            return self._jenkins_metadata["build_url"]
        return None

    @property
    def current_duration(self):
        return datetime.utcnow() - self.start_time

    def start_watchdog(self, queue_processor):
        if self._watch_dog is not None:
            raise Exception("Watchdog already started")
        self._watch_dog = asyncio.create_task(self.watchdog(queue_processor))

    def stop_watchdog(self):
        if self._watch_dog is None:
            return
        try:
            self._watch_dog.cancel()
        except asyncio.CancelledError:
            pass
        self._watch_dog = None

    def reset_keepalive(self):
        self.last_keepalive = datetime.utcnow()

    def append_log(self, name, data):
        try:
            f = self.log_files[name]
        except KeyError:
            f = self.log_files[name] = BytesIO()
            ret = True
        else:
            ret = False
        f.write(data)
        return ret

    async def watchdog(self, queue_processor):
        while True:
            await asyncio.sleep(self.KEEPALIVE_INTERVAL)
            duration = datetime.utcnow() - self.last_keepalive
            if duration > timedelta(seconds=(self.KEEPALIVE_INTERVAL * 2)):
                logging.warning(
                    "No keepalives received from %s for %s in %s, aborting.",
                    self.worker_name,
                    self.log_id,
                    duration,
                )
                result = self.create_result(
                    branch_url=self.queue_item.branch_url,
                    vcs_type=self.queue_item.vcs_type,
                    description=("No keepalives received in %s." % duration),
                    code="worker-timeout",
                    logfilenames=[],
                )
                await queue_processor.finish_run(self, result)
                break

    def kill(self) -> None:
        raise NotImplementedError(self.kill)

    def list_log_files(self):
        return list(self.log_files.keys())

    def get_log_file(self, name):
        try:
            return BytesIO(self.log_files[name].getvalue())
        except KeyError:
            raise FileNotFoundError

    def create_result(self, **kwargs):
        return JanitorResult(
            pkg=self.queue_item.package,
            suite=self.queue_item.suite,
            start_time=self.start_time,
            finish_time=datetime.utcnow(),
            log_id=self.log_id,
            worker_name=self.worker_name,
            worker_link=self.worker_link,
            **kwargs)

    def json(self) -> Any:
        """Return a JSON representation."""
        ret = {
            "queue_id": self.queue_item.id,
            "id": self.log_id,
            "package": self.queue_item.package,
            "suite": self.queue_item.suite,
            "estimated_duration": self.queue_item.estimated_duration.total_seconds()
            if self.queue_item.estimated_duration
            else None,
            "current_duration": self.current_duration.total_seconds(),
            "start_time": self.start_time.isoformat(),
            "worker": self.worker_name,
            "worker_link": self.worker_link,
            "logfilenames": list(self.list_log_files()),
            "jenkins": self._jenkins_metadata,
            "last-keepalive": self.last_keepalive.isoformat(
                timespec='seconds'),
        }
        return ret


def open_resume_branch(main_branch, suite_name, package, possible_hosters=None):
    try:
        hoster = get_hoster(main_branch, possible_hosters=possible_hosters)
    except UnsupportedHoster as e:
        # We can't figure out what branch to resume from when there's
        # no hoster that can tell us.
        logging.warning("Unsupported hoster (%s)", e)
        return None
    except HosterLoginRequired as e:
        logging.warning("No credentials for hoster (%s)", e)
        return None
    except ssl.SSLCertVerificationError as e:
        logging.warning("SSL error probing for hoster(%s)", e)
        return None
    except ConnectionError as e:
        logging.warning("Connection error opening resume branch (%s)", e)
        return None
    else:
        try:
            for option in [suite_name, ('%s/main' % suite_name), ('%s/main/%s' % (suite_name, package))]:
                (
                    resume_branch,
                    unused_overwrite,
                    unused_existing_proposal,
                ) = find_existing_proposed(
                        main_branch, hoster, suite_name,
                        preferred_schemes=['https', 'git', 'bzr'])
                if resume_branch:
                    break
        except NoSuchProject as e:
            logging.warning("Project %s not found", e.project)
            return None
        except PermissionDenied as e:
            logging.warning("Unable to list existing proposals: %s", e)
            return None
        except UnusableRedirect as e:
            logging.warning("Unable to list existing proposals: %s", e)
            return None
        else:
            return resume_branch


async def check_resume_result(conn: asyncpg.Connection, suite: str, resume_branch: Branch) -> Optional["ResumeInfo"]:
    row = await conn.fetchrow(
        "SELECT result, branch_name, review_status, "
        "array(SELECT row(role, remote_name, base_revision, revision) "
        "FROM new_result_branch WHERE run_id = run.id) AS result_branches "
        "FROM run "
        "WHERE suite = $1 AND revision = $2 AND result_code = 'success'",
        suite,
        resume_branch.last_revision().decode("utf-8"),
    )
    if row is not None:
        resume_branch_result = row['result']
        resume_review_status = row['review_status']
        resume_result_branches = [
            (role, name,
             base_revision.encode("utf-8") if base_revision else None,
             revision.encode("utf-8") if revision else None)
            for (role, name, base_revision, revision) in row['result_branches']]
    else:
        logging.warning(
            'Unable to find resume branch %r in database',
            resume_branch)
        return None
    if resume_review_status == "rejected":
        logging.info("Unsetting resume branch, since last run was rejected.")
        return None
    return ResumeInfo(
        resume_branch,
        resume_branch_result,
        resume_result_branches or [],
    )


class ResumeInfo(object):
    def __init__(self, branch, result, resume_result_branches):
        self.branch = branch
        self.result = result
        self.resume_result_branches = resume_result_branches

    @property
    def resume_branch_url(self):
        return full_branch_url(self.branch)

    def json(self):
        return {
            "result": self.result,
            "branch_url": self.resume_branch_url,
            "branches": [
                (fn, n, br.decode("utf-8"), r.decode("utf-8"))
                for (fn, n, br, r) in self.resume_result_branches
            ],
        }


def queue_item_env(queue_item):
    env = {}
    env["PACKAGE"] = queue_item.package
    if queue_item.upstream_branch_url:
        env["UPSTREAM_BRANCH_URL"] = queue_item.upstream_branch_url
    return env


def splitout_env(command):
    args = shlex.split(command)
    env = {}
    while len(args) > 0 and '=' in args[0]:
        (key, value) = args.pop(0).split('=', 1)
        env[key] = value
    return env, shlex_join(args)


def cache_branch_name(distro_config, role):
    if role != 'main':
        raise ValueError(role)
    return "%s/latest" % (distro_config.vendor or dpkg_vendor().lower())


async def store_run(
    conn: asyncpg.Connection,
    run_id: str,
    name: str,
    vcs_type: str,
    vcs_url: str,
    start_time: datetime,
    finish_time: datetime,
    command: str,
    description: str,
    instigated_context: Optional[str],
    context: Optional[str],
    main_branch_revision: Optional[bytes],
    result_code: str,
    revision: Optional[bytes],
    subworker_result: Optional[Any],
    suite: str,
    logfilenames: List[str],
    value: Optional[int],
    worker_name: str,
    worker_link: Optional[str],
    result_branches: Optional[List[Tuple[str, str, bytes, bytes]]] = None,
    result_tags: Optional[List[Tuple[str, bytes]]] = None,
    failure_details: Optional[Any] = None
):
    """Store a run.

    Args:
      run_id: Run id
      name: Package name
      vcs_type: VCS type
      vcs_url: Upstream branch URL
      start_time: Start time
      finish_time: Finish time
      command: Command
      description: A human-readable description
      instigated_context: Context that instigated this run
      context: Subworker-specific context
      main_branch_revision: Main branch revision
      result_code: Result code (as constant string)
      revision: Resulting revision id
      subworker_result: Subworker-specific result data (as json)
      suite: Suite
      logfilenames: List of log filenames
      value: Value of the run (as int)
      worker_name: Name of the worker
      worker_link: Link to worker URL
      result_branches: Result branches
      result_tags: Result tags
      failure_details: Result failure details
    """
    if result_tags is None:
        result_tags_updated = None
    else:
        result_tags_updated = [(n, r.decode("utf-8")) for (n, r) in result_tags]

    await conn.execute(
        "INSERT INTO run (id, command, description, result_code, "
        "start_time, finish_time, package, instigated_context, context, "
        "main_branch_revision, "
        "revision, result, suite, vcs_type, branch_url, logfilenames, "
        "value, worker, worker_link, result_tags, "
        "failure_details) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, "
        "$12, $13, $14, $15, $16, $17, $18, $19, $20, $21)",
        run_id,
        command,
        description,
        result_code,
        start_time,
        finish_time,
        name,
        instigated_context,
        context,
        main_branch_revision.decode("utf-8") if main_branch_revision else None,
        revision.decode("utf-8") if revision else None,
        subworker_result if subworker_result else None,
        suite,
        vcs_type,
        vcs_url,
        logfilenames,
        value,
        worker_name,
        worker_link,
        result_tags_updated,
        failure_details,
    )

    if result_branches:
        await conn.executemany(
            "INSERT INTO new_result_branch "
            "(run_id, role, remote_name, base_revision, revision) "
            "VALUES ($1, $2, $3, $4, $5)",
            [
                (run_id, role, remote_name, br.decode("utf-8") if br else None, r.decode("utf-8") if r else None)
                for (role, remote_name, br, r) in result_branches
            ],
        )


async def followup_run(config, database, policy, item, result: JanitorResult):
    if result.code == "success" and item.suite not in ("unchanged", "debianize"):
        async with database.acquire() as conn:
            run = await conn.fetchrow(
                "SELECT 1 FROM last_runs WHERE package = $1 AND revision = $2 AND result_code = 'success'",
                result.package, result.main_branch_revision.decode('utf-8')
            )
            if run is None:
                logging.info("Scheduling control run for %s.", item.package)
                await do_schedule_control(
                    conn,
                    item.package,
                    result.main_branch_revision,
                    estimated_duration=result.duration,
                    requestor="control",
                )
            # see if there are any packages that failed because
            # they lacked this one
            if getattr(result.builder_result, 'build_distribution', None) is not None:
                dependent_suites = [
                    suite.name for suite in config.suite
                    if result.builder_result.build_distribution in suite.debian_build.extra_build_distribution]
                runs_to_retry = await conn.fetch(
                    "SELECT package, suite FROM last_missing_apt_dependencies WHERE name = $1 AND suite = ANY($2::text[])",
                    item.package, dependent_suites)
                for run_to_retry in runs_to_retry:
                    await do_schedule(
                        conn, run_to_retry['package'],
                        bucket='missing-deps', requestor='schedule-missing-deps (now newer %s is available)' % item.package,
                        suite=run_to_retry['suite'])

    if result.followup_actions and result.code != 'success':
        from .missing_deps import schedule_new_package, schedule_update_package
        requestor = 'schedule-missing-deps (needed by %s)' % item.package
        async with database.acquire() as conn:
            for scenario in result.followup_actions:
                for action in scenario:
                    if action['action'] == 'new-package':
                        await schedule_new_package(
                            conn, action['upstream-info'],
                            policy,
                            requestor=requestor)
                    elif action['action'] == 'update-package':
                        await schedule_update_package(
                            conn, policy, action['package'], action['desired-version'],
                            requestor=requestor)
        from .missing_deps import reconstruct_problem, problem_to_upstream_requirement
        problem = reconstruct_problem(result.code, result.failure_details)
        if problem is not None:
            requirement = problem_to_upstream_requirement(problem)
        else:
            requirement = None
        if requirement:
            logging.info('TODO: attempt to find a resolution for %r', requirement)


class QueueProcessor(object):
    def __init__(
        self,
        database,
        policy,
        config,
        dry_run=False,
        logfile_manager=None,
        artifact_manager=None,
        vcs_manager=None,
        public_vcs_manager=None,
        use_cached_only=False,
        overall_timeout=None,
        committer=None,
        backup_artifact_manager=None,
        backup_logfile_manager=None,
    ):
        """Create a queue processor.
        """
        self.database = database
        self.policy = policy
        self.config = config
        self.dry_run = dry_run
        self.logfile_manager = logfile_manager
        self.artifact_manager = artifact_manager
        self.vcs_manager = vcs_manager
        self.public_vcs_manager = public_vcs_manager
        self.use_cached_only = use_cached_only
        self.topic_queue = Topic("queue", repeat_last=True)
        self.topic_result = Topic("result")
        self.overall_timeout = overall_timeout
        self.committer = committer
        self.active_runs: Dict[str, ActiveRun] = {}
        self.backup_artifact_manager = backup_artifact_manager
        self.backup_logfile_manager = backup_logfile_manager
        self.rate_limit_hosts = {}

    def status_json(self) -> Any:
        return {
            "processing": [
                active_run.json() for active_run in self.active_runs.values()
            ],
        }

    def register_run(self, active_run: ActiveRun) -> None:
        self.active_runs[active_run.log_id] = active_run
        self.topic_queue.publish(self.status_json())
        active_run_count.inc()
        packages_processed_count.inc()

    async def finish_run(self, item: state.QueueItem, result: JanitorResult) -> None:
        active_run_count.dec()
        run_result_count.labels(
            package=item.package,
            suite=item.suite,
            result_code=result.code).inc()
        build_duration.labels(package=item.package, suite=item.suite).observe(
            result.duration.total_seconds()
        )
        if not self.dry_run:
            async with self.database.acquire() as conn, conn.transaction():
                await store_run(
                    conn,
                    result.log_id,
                    item.package,
                    result.vcs_type,
                    result.branch_url,
                    result.start_time,
                    result.finish_time,
                    item.command,
                    result.description,
                    item.context,
                    result.context,
                    result.main_branch_revision,
                    result.code,
                    revision=result.revision,
                    subworker_result=result.subworker_result,
                    suite=item.suite,
                    logfilenames=result.logfilenames,
                    value=result.value,
                    worker_name=result.worker_name,
                    worker_link=result.worker_link,
                    result_branches=result.branches,
                    result_tags=result.tags,
                    failure_details=result.failure_details,
                )
                if result.builder_result:
                    await result.builder_result.store(conn, result.log_id)
                await conn.execute("DELETE FROM queue WHERE id = $1", item.id)
        self.topic_result.publish(result.json())
        try:
            del self.active_runs[result.log_id]
        except KeyError:
            pass
        self.topic_queue.publish(self.status_json())
        last_success_gauge.set_to_current_time()
        await followup_run(self.config, self.database, self.policy, item, result)

    def rate_limited(self, host, retry_after):
        rate_limited_count.labels(host=host).inc()
        self.rate_limit_hosts[host] = (
            retry_after or (datetime.now() + timedelta(seconds=DEFAULT_RETRY_AFTER)))

    def is_queue_item_rate_limited(self, url):
        host = urlutils.URL.from_string(url).host
        until = self.rate_limit_hosts.get(host)
        if not until:
            return False
        return until > datetime.now()

    async def next_queue_item(self, conn) -> Optional[state.QueueItem]:
        limit = len(self.active_runs) + 3
        async for item in state.iter_queue(conn, limit=limit):
            if self.is_queue_item_assigned(item.id):
                continue
            if self.is_queue_item_rate_limited(item.branch_url):
                continue
            return item
        return None

    def is_queue_item_assigned(self, queue_item_id: int) -> bool:
        """Check if a queue item has been assigned already."""
        for active_run in self.active_runs.values():
            if active_run.queue_item.id == queue_item_id:
                return True
        return False


@routes.get("/status", name="status")
async def handle_status(request):
    queue_processor = request.app['queue_processor']
    return web.json_response(queue_processor.status_json())


async def _find_active_run(request):
    queue_processor = request.app['queue_processor']
    run_id = request.match_info["run_id"]
    queue_id = request.query.get('queue_id')
    worker_name = request.query.get('worker_name')
    try:
        return queue_processor.active_runs[run_id]
    except KeyError:
        pass
    if not worker_name or not queue_id:
        raise web.HTTPNotFound(text="No such current run: %s" % run_id)
    async with queue_processor.database.acquire() as conn:
        queue_item = await state.get_queue_item(conn, int(queue_id))
    if queue_item is None:
        raise web.HTTPNotFound(
            text="Unable to find relevant queue item %r" % queue_id)
    active_run = ActiveRun(worker_name=worker_name, queue_item=queue_item)
    queue_processor.register_run(active_run)
    return active_run


@routes.get("/log/{run_id}", name="log-index")
async def handle_log_index(request):
    active_run = await _find_active_run(request)
    log_filenames = active_run.list_log_files()
    return web.json_response(log_filenames)


@routes.post("/kill/{run_id}", name="kill")
async def handle_kill(request):
    active_run = await _find_active_run(request)
    ret = active_run.json()
    active_run.kill()
    return web.json_response(ret)


@routes.get("/log/{run_id}/{filename}", name="log")
async def handle_log(request):
    queue_processor = request.app['queue_processor']
    run_id = request.match_info["run_id"]
    filename = request.match_info["filename"]

    if "/" in filename:
        return web.Response(text="Invalid filename %s" % filename, status=400)
    try:
        active_run = queue_processor.active_runs[run_id]
    except KeyError:
        return web.Response(text="No such current run: %s" % run_id, status=404)
    try:
        f = active_run.get_log_file(filename)
    except FileNotFoundError:
        return web.Response(text="No such log file: %s" % filename, status=404)

    try:
        response = web.StreamResponse(
            status=200, reason="OK", headers=[("Content-Type", "text/plain")]
        )
        await response.prepare(request)
        for chunk in f:
            await response.write(chunk)
        await response.write_eof()
    finally:
        f.close()
    return response


@routes.post("/assign", name="assign")
async def handle_assign(request):
    json = await request.json()
    worker = json["worker"]

    possible_transports = []
    possible_hosters = []

    span = aiozipkin.request_span(request)

    async def abort(active_run, code, description):
        result = active_run.create_result(
            branch_url=active_run.main_branch_url,
            vcs_type=active_run.vcs_type,
            code=code,
            description=description
        )
        await queue_processor.finish_run(active_run.queue_item, result)

    queue_processor = request.app['queue_processor']

    async with queue_processor.database.acquire() as conn:
        item = None
        while item is None:
            with span.new_child('sql:queue-item'):
                item = await queue_processor.next_queue_item(conn)
            if item is None:
                return web.json_response({'reason': 'queue empty'}, status=503)
            active_run = ActiveRun(
                worker_name=worker,
                queue_item=item,
                jenkins_metadata=json.get("jenkins"),
            )

            queue_processor.register_run(active_run)

            if item.branch_url is None:
                # TODO(jelmer): Try URLs in possible_salsa_urls_from_package_name
                await abort(active_run, 'not-in-vcs', "No VCS URL known for package.")
                item = None

        suite_config = get_suite_config(queue_processor.config, item.suite)

        # This is simple for now, since we only support one distribution.
        builder = get_builder(queue_processor.config, suite_config)

        with span.new_child('build-env'):
            build_env = await builder.build_env(conn, suite_config, item)

        try:
            with span.new_child('branch:open'):
                main_branch = await open_branch_with_fallback(
                    conn,
                    item.package,
                    item.vcs_type,
                    item.branch_url,
                    possible_transports=possible_transports,
                )
        except BranchRateLimited as e:
            host = urlutils.URL.from_string(item.branch_url).host
            logging.warning('Rate limiting for %s: %r', host, e)
            queue_processor.rate_limited(host, e.retry_after)
            await abort(active_run, 'pull-rate-limited', str(e))
            return web.json_response(
                {'reason': str(e)}, status=429, headers={
                    'Retry-After': e.retry_after or DEFAULT_RETRY_AFTER})
        except BranchOpenFailure:
            resume_branch = None
            vcs_type = item.vcs_type
        else:
            active_run.main_branch_url = full_branch_url(main_branch).rstrip('/')
            vcs_type = get_vcs_abbreviation(main_branch.repository)
            if not item.refresh:
                with span.new_child('resume-branch:open'):
                    resume_branch = await to_thread(
                        open_resume_branch,
                        main_branch,
                        suite_config.branch_name,
                        item.package,
                        possible_hosters=possible_hosters)
            else:
                resume_branch = None

        if vcs_type is not None:
            vcs_type = vcs_type.lower()

        if resume_branch is None and not item.refresh:
            with span.new_child('resume-branch:open'):
                resume_branch = await to_thread(
                    queue_processor.public_vcs_manager.get_branch,
                    item.package, '%s/%s' % (suite_config.name, 'main'), vcs_type)

        if resume_branch is not None:
            with span.new_child('resume-branch:check'):
                resume = await check_resume_result(conn, item.suite, resume_branch)
                if resume is not None:
                    if is_authenticated_url(resume.branch.user_url):
                        raise AssertionError('invalid resume branch %r' % (
                            resume.branch))
        else:
            resume = None

    try:
        with span.new_child('cache-branch:check'):
            cached_branch_url = queue_processor.public_vcs_manager.get_branch_url(
                item.package, cache_branch_name(queue_processor.config.distribution, "main"), vcs_type
            )
    except UnsupportedVcs:
        cached_branch_url = None

    env = {}
    env.update(queue_item_env(item))
    if queue_processor.committer:
        env.update(committer_env(queue_processor.committer))

    extra_env, command = splitout_env(item.command)
    env.update(extra_env)

    assignment = {
        "id": active_run.log_id,
        "description": "%s on %s" % (item.suite, item.package),
        "queue_id": item.id,
        "branch": {
            "url": active_run.main_branch_url,
            "subpath": item.subpath,
            "vcs_type": item.vcs_type,
            "cached_url": cached_branch_url,
        },
        "resume": resume.json() if resume else None,
        "build": {"target": builder.kind, "environment": build_env},
        "env": env,
        "command": command,
        "suite": item.suite,
        # TODO(jelmer): Don't let this depend on the suite name
        "force-build": suite_config.force_build,
        "vcs_manager": queue_processor.public_vcs_manager.base_urls.get(item.vcs_type),
    }

    with span.new_child('start-watchdog'):
        active_run.start_watchdog(queue_processor)
    return web.json_response(assignment, status=201)


@routes.post("/active-runs/{run_id}/log/{logname}", name="upload-log")
async def handle_upload_log(request):
    queue_processor = request.app['queue_processor']
    run_id = request.match_info['run_id']
    logname = request.match_info['logname']
    try:
        active_run = queue_processor.active_runs[run_id]
    except KeyError:
        logging.warning("No such current run: %s" % run_id)
        return web.json_response({'run_id': run_id}, status=404)

    async for data, _ in request.content.iter_chunks():
        if active_run.append_log(logname, data):
            # Make sure everybody is aware of the new log file.
            queue_processor.topic_queue.publish(queue_processor.status_json())
    active_run.reset_keepalive()

    return web.json_response({}, status=200)


@routes.get("/health", name="health")
async def handle_health(request):
    return web.Response(text="OK")


@routes.post("/active-runs/{run_id}/keepalive", name="keepalive")
async def handle_keepalive(request):
    queue_processor = request.app['queue_processor']
    run_id = request.match_info['run_id']
    try:
        active_run = queue_processor.active_runs[run_id]
    except KeyError:
        logging.warning("No such current run: %s" % run_id)
        return web.json_response({'run_id': run_id}, status=404)
    active_run.keepalive()
    return web.json_response({}, status=200)


@routes.post("/active-runs/{run_id}/finish", name="finish")
async def handle_finish(request):
    queue_processor = request.app['queue_processor']
    run_id = request.match_info["run_id"]
    active_run = queue_processor.active_runs.get(run_id)
    if active_run:
        active_run.stop_watchdog()
        queue_item = active_run.queue_item
        worker_name = active_run.worker_name
        worker_link = active_run.worker_link
        main_branch_url = active_run.main_branch_url
    else:
        queue_item = None
        worker_name = None
        worker_link = None
        main_branch_url = None

    reader = await request.multipart()
    worker_result = None

    filenames = []
    with tempfile.TemporaryDirectory() as output_directory:
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.filename == "result.json":
                worker_result = WorkerResult.from_json(await part.json())
            elif part.filename is None:
                return web.json_response(
                    {"reason": "Part without filename", "headers": dict(part.headers)},
                    status=400,
                )
            else:
                filenames.append(part.filename)
                output_path = os.path.join(output_directory, part.filename)
                with open(output_path, "wb") as f:
                    f.write(await part.read())

        if worker_result is None:
            return web.json_response({"reason": "Missing result JSON"}, status=400)

        if queue_item is None:
            async with queue_processor.database.acquire() as conn:
                queue_item = await state.get_queue_item(conn, worker_result.queue_id)
            if queue_item is None:
                return web.json_response(
                    {"reason": "Unable to find relevant queue item %r" % worker_result.queue_id}, status=404)
            if main_branch_url is None:
                main_branch_url = queue_item.branch_url
        if worker_name is None:
            worker_name = worker_result.worker_name

        logfilenames = await import_logs(
            output_directory,
            queue_processor.logfile_manager,
            queue_processor.backup_logfile_manager,
            queue_item.package,
            run_id,
        )

        result = JanitorResult(
            pkg=queue_item.package,
            suite=queue_item.suite,
            log_id=run_id,
            worker_name=worker_name,
            worker_link=worker_link,
            branch_url=main_branch_url,
            vcs_type=queue_item.vcs_type,
            worker_result=worker_result,
            logfilenames=logfilenames,
            )

        if worker_result.code is None:
            result.builder_result.from_directory(output_directory)

            artifact_names = result.builder_result.artifact_filenames()
            await store_artifacts_with_backup(
                queue_processor.artifact_manager,
                queue_processor.backup_artifact_manager,
                output_directory,
                run_id,
                artifact_names,
            )

    await queue_processor.finish_run(queue_item, result)
    return web.json_response(
        {"id": run_id, "filenames": filenames, "result": result.json()},
        status=201,
    )


async def create_app(queue_processor, tracer=None):
    app = web.Application()
    app.router.add_routes(routes)
    app['rate-limited'] = {}
    app['queue_processor'] = queue_processor
    setup_metrics(app)
    app.router.add_get(
        "/ws/queue", functools.partial(pubsub_handler, queue_processor.topic_queue),
        name="ws-queue"
    )
    app.router.add_get(
        "/ws/result", functools.partial(pubsub_handler, queue_processor.topic_result),
        name="ws-result"
    )
    aiozipkin.setup(app, tracer, skip_routes=[
        app.router['metrics'],
        app.router['ws-queue'],
        app.router['ws-result'],
        ]
    )
    return app


async def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(prog="janitor.runner")
    parser.add_argument(
        "--listen-address", type=str, help="Listen address", default="localhost"
    )
    parser.add_argument("--port", type=int, help="Listen port", default=9911)
    parser.add_argument(
        "--pre-check",
        help="Command to run to check whether to process package.",
        type=str,
    )
    parser.add_argument(
        "--post-check", help="Command to run to check package before pushing.", type=str
    )
    parser.add_argument(
        "--dry-run",
        help="Create branches but don't push or propose anything.",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--use-cached-only", action="store_true", help="Use cached branches only."
    )
    parser.add_argument(
        "--config", type=str, default="janitor.conf", help="Path to configuration."
    )
    parser.add_argument(
        "--overall-timeout",
        type=int,
        default=None,
        help="Overall timeout per run (in seconds).",
    )
    parser.add_argument(
        "--backup-directory",
        type=str,
        default=None,
        help=(
            "Backup directory to write files to if artifact or log "
            "manager is unreachable"
        ),
    )
    parser.add_argument(
        "--public-vcs-location", type=str, default="https://janitor.debian.net/",
        help="Public vcs location (used for URLs handed to worker)"
    )
    parser.add_argument(
        "--vcs-store-url", type=str, default="http://localhost:9923/",
        help="URL to vcs store"
    )
    parser.add_argument(
        "--policy", type=str, default="policy.conf", help="Path to policy."
    )
    parser.add_argument("--gcp-logging", action='store_true', help='Use Google cloud logging.')
    args = parser.parse_args()

    if args.gcp_logging:
        import google.cloud.logging
        client = google.cloud.logging.Client()
        client.get_default_handler()
        client.setup_logging()
    else:
        logging.basicConfig(level=logging.INFO)

    debug.set_debug_flags_from_config()

    with open(args.config, "r") as f:
        config = read_config(f)

    state.DEFAULT_URL = config.database_location
    public_vcs_manager = get_vcs_manager(args.public_vcs_location)
    if args.vcs_store_url:
        vcs_manager = get_vcs_manager(args.vcs_store_url)
    else:
        vcs_manager = public_vcs_manager

    endpoint = aiozipkin.create_endpoint("janitor.runner", ipv4=args.listen_address, port=args.port)
    if config.zipkin_address:
        tracer = await aiozipkin.create(config.zipkin_address, endpoint, sample_rate=1.0)
    else:
        tracer = await aiozipkin.create_custom(endpoint)
    trace_configs = [aiozipkin.make_trace_config(tracer)]

    logfile_manager = get_log_manager(config.logs_location, trace_configs=trace_configs)
    artifact_manager = get_artifact_manager(config.artifact_location, trace_configs=trace_configs)

    loop = asyncio.get_event_loop()

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(artifact_manager)
        if args.backup_directory:
            backup_logfile_directory = os.path.join(args.backup_directory, "logs")
            backup_artifact_directory = os.path.join(args.backup_directory, "artifacts")
            if not os.path.isdir(backup_logfile_directory):
                os.mkdir(backup_logfile_directory)
            if not os.path.isdir(backup_artifact_directory):
                os.mkdir(backup_artifact_directory)
            backup_artifact_manager = LocalArtifactManager(backup_artifact_directory)
            await stack.enter_async_context(backup_artifact_manager)
            backup_logfile_manager = FileSystemLogFileManager(backup_logfile_directory)
            loop.create_task(
                upload_backup_artifacts(
                    backup_artifact_manager, artifact_manager, timeout=60 * 15
                )
            )
        else:
            backup_artifact_manager = None
            backup_logfile_manager = None
        db = state.Database(config.database_location)
        with open(args.policy, 'r') as f:
            policy = read_policy(f)
        queue_processor = QueueProcessor(
            db,
            policy,
            config,
            args.dry_run,
            logfile_manager,
            artifact_manager,
            vcs_manager,
            public_vcs_manager,
            args.use_cached_only,
            overall_timeout=args.overall_timeout,
            committer=config.committer,
            backup_artifact_manager=backup_artifact_manager,
            backup_logfile_manager=backup_logfile_manager,
        )

        app = await create_app(queue_processor, tracer=tracer)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, args.listen_address, port=args.port)
        await site.start()
        while True:
            await asyncio.sleep(3600)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
