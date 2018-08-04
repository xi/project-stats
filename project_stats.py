from functools import total_ordering
import argparse
import asyncio
import logging
import os
import re
import subprocess
import sys

from dateutil import parser as dt
import aiohttp
import yaml

try:
    from cheesecake.cheesecake_index import Cheesecake
except ImportError:
    Cheesecake = None

__version__ = '2.0.0'

SOURCES = ['github', 'gitlab', 'local', 'pypi', 'npm', 'travis', 'firefox']

KEYS = [
    'name',
    'description',
    'version',
    'homepage',
    'created',
    'updated',
    'license',
    'language',
    'tests',
    'commit_count',
    'file_count',
    'unstaged_changes',
    'uncommited_changes',
    'up_to_date',
    'contributors',
    'downloads',
    'open_issues',
    'open_pull_requests',
    'forks_count',
    'stargazers_count',
    'subscribers_count',
    'watchers_count',
    'cheesecake_index',
]


def aiorun(future):
    """Return value of a future synchronously."""
    container = []

    @asyncio.coroutine
    def wrapper():
        result = yield from future
        container.append(result)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(wrapper())

    return container[0]


def r_get(d, *keys):
    """Recursively get key from dict or return None."""
    if len(keys) == 0:
        return d
    elif keys[0] in d:
        return r_get(d[keys[0]], *keys[1:])


@total_ordering
class Claims(object):
    def __init__(self):
        self._list = []

    def _index(self, key):
        i = 0
        for k, value in self._list:
            if key == k:
                return i
            i += 1
        raise KeyError

    def add(self, value, source):
        if not value and value != 0:
            return
        try:
            i = self._index(value)
        except KeyError:
            self._list.append((value, []))
            i = -1
        self._list[i][1].append(source)

    def values(self):
        return [value for value, sources in self._list]

    def format(self, show_sources=True):
        def _format_claim(value, sources):
            s = str(value)
            if show_sources:
                s += ' (%s)' % ', '.join(sources)
            return s
        return '; '.join([_format_claim(v, srcs) for v, srcs in self._list])

    def __lt__(self, other):
        return self.values() < other.values()


class ClaimsDict(object):
    def __init__(self, keys, short=9):
        self._keys = keys
        self._short = short
        self._data = {}

    def update(self, data, source):
        for key in data:
            if key not in self._keys:
                raise KeyError(key)
        if source not in self._data:
            self._data[source] = {}
        self._data[source].update(data)

    def __getitem__(self, key):
        if key not in self._keys:
            raise KeyError(key)
        claims = Claims()
        for source, data in self._data.items():
            if key in data:
                claims.add(data[key], source)
        return claims

    def get(self, key, default=None):
        try:
            return self.__getitem__(key)
        except KeyError:
            return default

    def format(self, short=False, indent=0, show_sources=True):
        keys = self._keys[:self._short] if short else self._keys
        lines = []
        for key in keys:
            value = self.get(key)
            formated_value = value.format(show_sources=show_sources)
            if not formated_value:
                continue
            lines.append(' ' * indent + '%s: %s' % (key, formated_value))
        return '\n'.join(lines)


def cheesecake_index(name):
    if Cheesecake is not None:
        # does not seem to be meant to be used as a library
        c = Cheesecake(name=name, quiet=True, lite=True)
        value = c.index.compute_with(c)
        max_value = c.index.max_value
        c.cleanup()
        return value * 100 / max_value
    else:
        return None


async def get_json(url, user=None, token=None):
    assert not (user is None) ^ (token is None)

    if user is not None:
        # FIXME: not very robust
        url += '&' if '?' in url else '?'
        url += 'login=%s&token=%s' % (user, token)

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.json()


async def get_github(name, user=None, token=None):
    api_url = 'https://api.github.com/repos/' + name

    async def _get_json(url):
        data = await get_json(url, user=user, token=token)
        if 'documentation_url' in data:
            raise aiohttp.ClientError(data['documentation_url'])
        return data

    async def get_latest_tag():
        data = await _get_json(api_url + '/tags?per_page=100')
        tags = [tag['name'] for tag in data]
        if len(tags) > 0:
            return max(tags, key=lambda tag: tag.lstrip('v'))
        else:
            return

    async def get_open_pull_requests():
        data = await _get_json(api_url + '/pulls')
        return len(data)

    data, version, pulls = await asyncio.gather(
        _get_json(api_url),
        get_latest_tag(),
        get_open_pull_requests())

    return {
        'name': data['name'],
        'description': data['description'],
        'license': data.get('license', {}).get('spdx_id'),
        'created': dt.parse(data['created_at']),
        'updated': dt.parse(data['updated_at']),
        'homepage': data['homepage'],
        'language': data['language'],
        'watchers_count': data['watchers_count'],
        'stargazers_count': data['stargazers_count'],
        'subscribers_count': data['subscribers_count'],
        'forks_count': data['forks_count'],
        'open_issues': data['open_issues'],
        'open_pull_requests': pulls,
        'version': version,
    }


async def get_gitlab(_id, token=None):
    async def _get_json(path):
        api_url = 'https://gitlab.com/api/v3/projects/' + _id + path
        if token is not None:
            if '?' in api_url:
                api_url += '&private_token=' + token
            else:
                api_url += '?private_token=' + token
        return await get_json(api_url)

    data, issues, pulls = await asyncio.gather(
        _get_json(''),
        _get_json('/issues?state=opened'),
        _get_json('/merge_requests?state=opened'))

    return {
        'name': data['name'],
        'description': data['description'],
        'homepage': data['web_url'],
        'created': dt.parse(data['created_at']),
        'updated': dt.parse(data['last_activity_at']),
        'forks_count': data['forks_count'],
        'watchers_count': data['star_count'],
        'open_issues': len(issues),
        'open_pull_requests': len(pulls),
    }


async def get_local(path):
    def git(cmd, *args):
        _cmd = ['git', '-C', path, cmd] + list(args)
        return subprocess.check_output(_cmd).decode('utf8')

    def get_latest_tag():
        tags = git('tag').splitlines()
        if len(tags) > 0:
            return max(tags, key=lambda tag: tag.lstrip('v'))

    def get_rev_datetime(rev):
        return git('show', '-s', '--format=%ai', rev).rstrip()

    tail = git('rev-list', 'HEAD').splitlines()[-1]

    return {
        'name': os.path.basename(os.path.abspath(path)),
        'file_count': len(git('ls-files').splitlines()),
        'unstaged_changes': 'Changes not staged for commit' in git('status'),
        'uncommited_changes': 'Changes to be committed' in git('status'),
        'up_to_date': 'Your branch is up-to-date with' in git('status'),
        'commit_count': len(git('rev-list', 'HEAD').splitlines()),
        'version': get_latest_tag(),
        'contributors': len(git('shortlog', '-s').splitlines()),
        'created': dt.parse(get_rev_datetime(tail)),
        'updated': dt.parse(get_rev_datetime('HEAD')),
    }


async def get_pypi(name):
    data = await get_json('https://pypi.org/pypi/{}/json'.format(name))
    return {
        'version': data['info']['version'],
        'description': data['info']['summary'],
        'downloads': data['info']['downloads'],
        'name': data['info']['name'],
        'license': data['info']['license'],
        'homepage': data['info']['home_page'],
        'cheesecake_index': cheesecake_index(data['info']['name']),
    }


async def get_npm(name):
    process = await asyncio.create_subprocess_exec(
        'npm', 'view', name,
        'name',
        'version',
        'homepage',
        'description',
        'license',
        'time.created',
        'time.modified',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        return
    s = stdout.decode('utf8')

    data = {}
    for line in s.splitlines():
        m = re.match("(.*) = '(.*)'", line)
        if m:
            key = m.groups()[0]
            value = m.groups()[1]

            if key == 'time.created':
                data['created'] = dt.parse(value)
            elif key == 'time.modified':
                data['updated'] = dt.parse(value)
            else:
                data[key] = value

    return data


async def get_travis(name):
    data = await get_json('https://api.travis-ci.org/repos/' + name)
    return {
        'description': data['description'],
        'tests': data['last_build_result'] == 0,
    }


async def get_firefox(name):
    def get_us(d, key):
        return d.get(key, {}).get('en-US')

    api_url = 'https://addons.mozilla.org/api/v3/addons/addon/' + name
    data = await get_json(api_url)

    return {
        'name': get_us(data, 'name'),
        'description': get_us(data, 'summary'),
        'version': data.get('current_version', {}).get('version'),
        'homepage': get_us(data, 'homepage'),
        'updated': data.get('last_updated'),
        'license': get_us(data.get('current_version', {}), 'license'),
        'downloads': data.get('weekly_downloads'),
        'subscribers_count': data.get('average_daily_users'),
    }


async def get_source(key, source, config, claims):
    fn = globals()['get_' + key]
    if key == 'github':
        future = fn(
            source,
            user=r_get(config, 'github', 'user'),
            token=r_get(config, 'github', 'token'))
    elif key == 'gitlab':
        future = fn(source, token=r_get(config, 'gitlab', 'token'))
    else:
        future = fn(source)

    try:
        data = await future
        claims.update(data, key)
    except Exception as e:
        message = 'Error while gathering stats for %s from %s: %s',
        logging.error(message, key, source, e)


async def get_project(key, project, config):
    claims = ClaimsDict(KEYS)
    futures = []
    for source in SOURCES:
        if source in project:
            futures.append(get_source(source, project[source], config, claims))
    await asyncio.gather(*futures)
    return claims


def get_projects(projects_config, config):
    projects_list = aiorun(asyncio.gather(*[
        get_project(key, project, config)
        for key, project in projects_config.items()]))

    projects = {}
    for key, project in zip(projects_config.keys(), projects_list):
        if project is not None:
            projects[key] = project
    return projects


def select_config(args):
    if args.config is not None:
        return os.path.expanduser(args.config)
    else:
        choices = [
            os.path.abspath('./projects.yml'),
            os.path.abspath('./.projects.yml'),
            os.path.expanduser('~/.config/projects.yml'),
            os.path.expanduser('~/.projects.yml'),
        ]

        for path in choices:
            if os.path.exists(path):
                return path

        print('No config file available. Tried %s.' % ', '.join(choices))
        sys.exit(1)


def load_config(path):
    with open(path) as fh:
        return yaml.safe_load(fh)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('query', nargs='?', help='optionally filter projects')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument(
        '-l', '--list',
        action='store_true',
        help='only list projects; do not show any stats')
    parser.add_argument(
        '-s', '--short',
        action='store_true',
        help='show only basic stats')
    parser.add_argument('-c', '--config')
    parser.add_argument(
        '-z', '--sort',
        metavar='KEY',
        help='sort by key')
    parser.add_argument(
        '-S', '--show-sources',
        action='store_true',
        help='show a source for each claim')

    return parser.parse_args()


def main():
    args = parse_args()
    config = load_config(select_config(args))

    keys = config['projects'].keys()
    if args.query is not None:
        keys = [k for k in keys if args.query.lower() in k.lower()]

    if args.list and args.sort is None:
        for key in keys:
            print(key)
    else:
        projects_config = {key: config['projects'][key] for key in keys}
        projects = get_projects(projects_config, config)
        keys = [k for k in keys if k in projects]

        if args.sort is not None:
            keys.sort(key=lambda k: projects[k][args.sort])

        for key in keys:
            if args.list:
                claim = projects[key][args.sort]
                print(key, claim.format(show_sources=False))
            else:
                claims = projects[key]
                print('%s\n%s\n' % (key, claims.format(
                    indent=2,
                    short=args.short,
                    show_sources=args.show_sources)))


if __name__ == '__main__':
    main()
