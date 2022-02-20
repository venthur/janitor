This repository contains the setup for a "Janitor" bot. This is basically
a platform for running silver-platter continuously on a specific set
of packages.


Any code that is not related to the platform but to actually making changes
should probably live in either ``silver-platter``, ``lintian-brush`` or
``breezy``.

There are currently several instances of the Janitor running. For their configuration, see:

* [Debian Janitor](https://janitor.debian.net/) - Setup at https://salsa.debian.org/jelmer/janitor.debian.net
* [Kali Janitor](https://janitor.kali.org/) - Configuration repository is private
* [Upstream Janitor](https://janitor.jelmer.uk/) - Setup at https://github.com/jelmer/janitor.jelmer.uk

Philosophy
==========

There are some straightforward changes to code that can be made
using scripting. The janitor's job is to opportunistically make those changes
when it is certain it can do so with a high confidence, and to back off
otherwise.

The janitor continuously tries to run changes on the set of repositories it
knows about. It tries to be clever about scheduling those operations that
are more likely to yield results and be published (i.e. merged or pushed).

Design
======

The janitor is made up out of multiple components. The majority of these
are not Debian-specific. The janitor is built on top of
[silver-platter](https://github.com/jelmer/silver-platter) and relies
on that project for most of the grunt work.

There are several cron jobs that run daily:

* the *package_metadata* syncer imports package metadata from UDD
* the *candidate* syncer determines candidates
* the *scheduler* determines what packages are ready for processing
  based on lintian, vcswatch and upstream data, and queues them.

Several permanently running jobs:

* the *publisher* proposes or pushes changes that have been successfully
  created and built previously, and which can provide VCS diffs
* the *vcs store* manages and stores VCS repositories (git, bzr)
* the *runner* processes the queue, kicks off workers for
  each package and stores the results.
* one or more *workers* which are responsible for actual generating and
  building changes.
* an *archiver* that takes care of managing the apt archives and publishes them
* a *site* job that renders the web site
* the *differ* takes care of running e.g. debdiff or diffoscope between binary runs

There are no requirements that these jobs run on the same machine, but they are
expected to have secure network access to each other.

Every job runs a HTTP server to allow API requests and use of /metrics, for
prometheus monitoring.

Workers are fairly naive; they simply run a ``silver-platter`` subcommand
to create branches and they build the resulting branches. The runner
then fetches the results from each run and (if the run was successful)
uploads the .debs and optionally proposes a change.

The publisher is responsible for enforcing rate limiting, i.e. making sure
that there are no more than X pull requests open per maintainer.

Worker
======
The actual changes are made by various worker scripts that implement
the [silver-platter protocol](https://github.com/jelmer/silver-platter/blob/master/devnotes/mutators.rst).

Web site
========

The web site is served by the ``janitor.site`` module using jinja2 templates
from the ``janitor/site/templates/`` subdirectory.

Installation
============

There are two common ways of deployign a new janitor instance.

 * On top of kubernetes (see the configuration for the Debian & Upstream janitor)
 * Using ansible, based on the playbooks in the ``ansible/`` directory

Contributing
============

The easiest way to get started with contributing to the Janitor is to work on
identifying issues and adding fixers for lintian-brush. There is
[a guide](https://salsa.debian.org/jelmer/lintian-brush/-/blob/master/doc/fixer-writing-guide.rst)
on identifying good candidates and writing fixers in the lintian-brush
repository.

If you're interested in working on adding another suite, see
[adding-a-new-suite](devnotes/adding-a-new-suite.rst).

Some of us hang out in the ``#debian-janitor`` IRC channel on OFTC
(irc.oftc.net).
