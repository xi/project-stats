from functools import total_ordering
import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys

from dateutil import parser as dt
from rediscache import cached, async_cached
import aiohttp
import yaml

try:
    from cheesecake.cheesecake_index import Cheesecake
except ImportError:
    Cheesecake = None

__version__ = '1.1.1'

SOURCES = ['github', 'gitlab', 'local', 'pypi', 'bower', 'npm', 'travis']

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


@cached('xi-project-cheesecake', ttl=36000)
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


@async_cached('xi-project-bower', ttl=3600)
@asyncio.coroutine
def get_bower_info(name):
    process = yield from asyncio.create_subprocess_exec(
        'bower', 'info', name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    stdout, stderr = yield from process.communicate()
    if process.returncode != 0:
        return
    s = stdout.decode('utf8')

    # re handles \n specially, so it is replaced by \t
    s = '\t'.join(s.splitlines())

    # strip uninteresting information
    s = re.sub('.*\t{', '{', s)
    s = re.sub('\t}.*', '\t}', s)

    # this is Javascript object syntax, not strict JSON
    s = re.sub('\t( *)([a-z]*): ', '\t\\1"\\2": ', s)
    s = s.replace('\'', '"')

    return json.loads(s)


@async_cached('xi-project', ttl=3600)
@asyncio.coroutine
def get_json(url, user=None, password=None):
    assert not (user is None) ^ (password is None)

    if user is None:
        req = yield from aiohttp.get(url)
    else:
        req = yield from aiohttp.get(
            url, auth=aiohttp.BasicAuth(user, password))

    data = yield from req.json()
    return data


@asyncio.coroutine
def get_github(url, user=None, password=None):
    api_url = re.sub(
        'https?://github.com', 'https://api.github.com/repos', url)

    @asyncio.coroutine
    def _get_json(url):
        data = yield from get_json(url, user=user, password=password)
        if 'documentation_url' in data:
            raise aiohttp.ClientError(data['documentation_url'])
        return data

    @asyncio.coroutine
    def get_latest_tag():
        data = yield from _get_json(api_url + '/tags?per_page=100')
        tags = [tag['name'] for tag in data]
        if len(tags) > 0:
            return max(tags, key=lambda tag: tag.lstrip('v'))
        else:
            return

    @asyncio.coroutine
    def get_open_pull_requests():
        data = yield from _get_json(api_url + '/pulls')
        return len(data)

    data, version, pulls = yield from asyncio.gather(
        _get_json(api_url),
        get_latest_tag(),
        get_open_pull_requests())

    return {
        'name': data['name'],
        'description': data['description'],
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


@asyncio.coroutine
def get_gitlab(_id, token=None):
    @asyncio.coroutine
    def _get_json(path):
        api_url = 'https://gitlab.com/api/v3/projects/' + _id + path
        if token is not None:
            if '?' in api_url:
                api_url += '&private_token=' + token
            else:
                api_url += '?private_token=' + token
        return get_json(api_url)

    data, issues, pulls = yield from asyncio.gather(
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


@asyncio.coroutine
def get_local(path):
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


@asyncio.coroutine
def get_pypi(url):
    data = yield from get_json(url + '/json')
    return {
        'version': data['info']['version'],
        'description': data['info']['summary'],
        'downloads': data['info']['downloads'],
        'name': data['info']['name'],
        'license': data['info']['license'],
        'homepage': data['info']['home_page'],
        'cheesecake_index': cheesecake_index(data['info']['name']),
    }


@asyncio.coroutine
def get_bower(name):
    data = yield from get_bower_info(name)
    if data is None:
        return {}
    else:
        return {
            'name': data['name'],
            'version': data.get('version'),
            'homepage': data.get('homepage'),
            'description': data.get('description'),
            'license': data.get('license'),
        }


@async_cached('xi-project-npm', ttl=3600)
@asyncio.coroutine
def get_npm(name):
    process = yield from asyncio.create_subprocess_exec(
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
    stdout, stderr = yield from process.communicate()
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


@asyncio.coroutine
def get_travis(url):
    api_url = re.sub(
        'https?://travis-ci.org', 'https://api.travis-ci.org/repos', url)
    data = yield from get_json(api_url)
    return {
        'description': data['description'],
        'tests': data['last_build_result'] == 0,
    }


@asyncio.coroutine
def get_source(key, source, config, claims):
    fn = globals()['get_' + key]
    if key == 'github':
        future = fn(
            source,
            user=r_get(config, 'github', 'user'),
            password=r_get(config, 'github', 'password'))
    elif key == 'gitlab':
        future = fn(source, token=r_get(config, 'gitlab', 'token'))
    else:
        future = fn(source)

    try:
        data = yield from future
        claims.update(data, key)
    except Exception as e:
        message = 'Error while gathering stats for %s from %s: %s',
        logging.error(message, key, source, e)


@asyncio.coroutine
def get_project(key, project, config):
    claims = ClaimsDict(KEYS)
    futures = []
    for source in SOURCES:
        if source in project:
            futures.append(get_source(source, project[source], config, claims))
    yield from asyncio.gather(*futures)
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
