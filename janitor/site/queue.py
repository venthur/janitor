#!/usr/bin/python3
# Copyright (C) 2019 Jelmer Vernooij <jelmer@jelmer.uk>
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
import asyncio
import sys

from janitor import state
from janitor.site import env


def lintian_tag_link(tag):
    return '<a href="https://lintian.debian.org/tags/%s.html">%s</a>' % (
        tag, tag)


async def get_queue(only_command=None, limit=None):
    data = []

    async for queue_id, branch_url, run_env, command in (
            state.iter_queue(limit=limit)):
        if only_command is not None and command != only_command:
            continue
        expecting = None
        if command[0] == 'new-upstream':
            if '--snapshot' in command:
                description = 'New upstream snapshot'
            else:
                description = 'New upstream'
                if run_env.get('CONTEXT'):
                    expecting = 'expecting to merge %s' % run_env['CONTEXT']
        elif command[0] == 'lintian-brush':
            description = 'Lintian fixes'
            if run_env.get('CONTEXT'):
                expecting = (
                    'expecting to fix: ' +
                    ', '.join(
                        map(lintian_tag_link, run_env['CONTEXT'].split(' '))))
        else:
            raise AssertionError('invalid command %s' % command)
        if only_command is not None:
            description = expecting
        elif expecting is not None:
            description += ", " + expecting
        data.append((run_env['PACKAGE'], description))

    return data


async def write_queue(only_command=None, limit=None):
    template = env.get_template('queue.html')
    return await template.render_async(
        queue=await get_queue(only_command, limit))


if __name__ == '__main__':
    parser = argparse.ArgumentParser('report-queue')
    parser.add_argument(
        '--command', type=str, help='Only display queue for specified command')
    parser.add_argument(
        '--limit', type=int, help='Limit to this number of entries',
        default=100)
    args = parser.parse_args()

    loop = asyncio.get_event_loop()
    sys.stdout.write(loop.run_until_complete(write_queue()))
